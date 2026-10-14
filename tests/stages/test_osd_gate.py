"""ROTATE_MIN_CONFIDENCE gate: accept hoa-band, reject plant-band."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from yokaze.lib.brod.osd import _gated_from_text, agreed_cjk_rotation
from yokaze.lib.brod.rotation_plan import resolve_rotation_plan


def _osd(rotate: int, conf: float) -> str:
    return f"Rotate: {rotate}\nOrientation confidence: {conf}\n"


class OsdGateTests(unittest.TestCase):
    def test_agreed_japanese_votes_keep_the_angle_under_the_hard_gate(self) -> None:
        """Charm: every turn says 270 and Japanese, each at least 1.0."""
        votes = [
            (270, 1.0, "Japanese"),
            (270, 1.37, "Japanese"),
            (270, 1.09, "Japanese"),
            (270, 1.29, "Japanese"),
        ]
        self.assertEqual(agreed_cjk_rotation(votes), (270, 1.37))

    def test_split_or_weak_votes_do_not_agree(self) -> None:
        plant = [
            (90, 0.03, "Japanese"),
            (90, 0.93, "Japanese"),
            (270, 0.01, "Japanese"),
            (90, 0.86, "Latin"),
        ]
        colombia = [
            (90, 1.57, "Latin"),
            (270, 1.27, "Latin"),
            (90, 2.66, "Latin"),
            (90, 0.9, "Latin"),
        ]
        self.assertIsNone(agreed_cjk_rotation(plant))
        self.assertIsNone(agreed_cjk_rotation(colombia))
        self.assertIsNone(
            agreed_cjk_rotation(
                [(270, 0.9, "Japanese"), (270, 0.9, "Japanese"), (270, 0.9, "Han")]
            )
        )

    def test_gate_accepts_hoa_four_way_1_88(self) -> None:
        rot, conf, reason = _gated_from_text(_osd(90, 1.88), 1.8)
        self.assertEqual(reason, "")
        self.assertEqual(rot, 90)
        self.assertEqual(conf, 1.88)

    def test_gate_rejects_plant_four_way_0_93(self) -> None:
        rot, conf, reason = _gated_from_text(_osd(90, 0.93), 1.8)
        self.assertEqual(rot, 90)  # angle kept for soft fallback
        self.assertEqual(reason, "low_conf")
        self.assertEqual(conf, 0.93)

    def test_gate_rejects_wrong_low_conf_colombia_adaptive(self) -> None:
        """colombia p2 adaptive 0° @ 0.63 is wrong; 4-way 90° @ 2.66 is already in."""
        rot, _, reason = _gated_from_text(_osd(0, 0.63), 1.8)
        self.assertEqual(rot, 0)
        self.assertEqual(reason, "low_conf")

    def test_soft_adaptive_when_four_way_too_few(self) -> None:
        """Sparse card: adaptive below hard gate, 4-way starved → soft accept."""
        from yokaze.lib.brod.osd import page_orientation

        calls = {"n": 0}

        def fake_adaptive(tiff, min_confidence):
            calls["n"] += 1
            if min_confidence <= 0.0:
                return 180, 0.62, ""
            return 180, 0.62, "low_conf"

        def fake_four(tiff, min_confidence):
            return None, None, "too_few"

        with (
            patch("yokaze.lib.brod.osd.adaptive_osd", fake_adaptive),
            patch("yokaze.lib.brod.osd.four_way_osd", fake_four),
        ):
            rot = page_orientation(Path("page.tiff"), 1.8, label="p")
        self.assertEqual(rot, 180)

    def test_soft_skips_when_four_way_has_low_conf_vote(self) -> None:
        """plant-class: 4-way parsed but below gate — do not soft-accept adaptive."""
        from yokaze.lib.brod.osd import page_orientation

        def fake_adaptive(tiff, min_confidence):
            return 180, 0.86, "low_conf"

        def fake_four(tiff, min_confidence):
            return 90, 0.93, "low_conf"

        with (
            patch("yokaze.lib.brod.osd.adaptive_osd", fake_adaptive),
            patch("yokaze.lib.brod.osd.four_way_osd", fake_four),
        ):
            rot = page_orientation(Path("page.tiff"), 1.8, label="p")
        self.assertIsNone(rot)

    def test_resolve_rotation_plan_reads_1_8_default(self) -> None:
        seen: list[float] = []

        def fake_own(raw, min_conf, *, label):
            seen.append(min_conf)
            return 90

        with patch(
            "yokaze.lib.brod.rotation_plan._own_rotation_after_rough",
            fake_own,
        ):
            plan = resolve_rotation_plan([Path("page-0001.tiff")])
        self.assertEqual(plan, [90])
        self.assertEqual(seen, [1.8])

    def test_unreadable_front_copies_90_from_other_front(self) -> None:
        """Two card fronts (odd pages): copy, do not flip 90→270."""
        own = {Path("page-0001.tiff"): None, Path("page-0003.tiff"): 90}

        def fake_own(raw, min_conf, *, label):
            return own[raw]

        with patch(
            "yokaze.lib.brod.rotation_plan._own_rotation_after_rough",
            fake_own,
        ):
            plan = resolve_rotation_plan(
                [Path("page-0001.tiff"), Path("page-0003.tiff")]
            )
        self.assertEqual(plan, [90, 90])

    def test_unreadable_front_copies_90_across_batch_filename(self) -> None:
        own = {
            Path("batch-01-page-0001.tiff"): None,
            Path("batch-01-page-0003.tiff"): 90,
        }

        def fake_own(raw, min_conf, *, label):
            return own[raw]

        with patch(
            "yokaze.lib.brod.rotation_plan._own_rotation_after_rough",
            fake_own,
        ):
            plan = resolve_rotation_plan(list(own))
        self.assertEqual(plan, [90, 90])

    def test_unreadable_front_still_flips_from_back(self) -> None:
        """Same-sheet duplex: opposite page parity still flips 270→90."""
        own = {Path("page-0001.tiff"): None, Path("page-0002.tiff"): 270}

        def fake_own(raw, min_conf, *, label):
            return own[raw]

        with patch(
            "yokaze.lib.brod.rotation_plan._own_rotation_after_rough",
            fake_own,
        ):
            plan = resolve_rotation_plan(
                [Path("page-0001.tiff"), Path("page-0002.tiff")]
            )
        self.assertEqual(plan, [90, 270])


if __name__ == "__main__":
    unittest.main()
