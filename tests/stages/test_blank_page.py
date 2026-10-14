"""Blank-page knobs: catch pale card backs, leave sparse-content fronts."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from yokaze.lib.brod.blanks import is_blank_page, partition_blank_pages
from yokaze.ui.backend import CORPUS


def _raw(case: str, page: int) -> Path:
    return CORPUS / case / "raw" / f"page-{page:04d}.tiff"


class BlankPageTests(unittest.TestCase):
    def test_watch_white_card_backs_are_blank(self) -> None:
        """Pale hang-tag versos sit just above the old 8 / 0.025 gate."""
        for page in (2, 4):
            raw = _raw("watch", page)
            if not raw.is_file():
                self.skipTest(f"missing {raw}")
            self.assertTrue(is_blank_page(raw), f"watch p{page}")

    def test_known_blank_backs_stay_blank(self) -> None:
        for case, page in (
            ("hoa", 2),
            ("hayakawa", 2),
            ("aeon", 2),
            ("vote", 2),
            ("burger", 4),
        ):
            raw = _raw(case, page)
            if not raw.is_file():
                self.skipTest(f"missing {raw}")
            self.assertTrue(is_blank_page(raw), f"{case} p{page}")

    def test_sparse_content_fronts_are_not_blank(self) -> None:
        """hayakawa/covert/watch fronts + shipping p2 (low ink, high edge)."""
        for case, page in (
            ("hayakawa", 1),
            ("covert", 1),
            ("watch", 1),
            ("watch", 3),
            ("shipping", 2),
        ):
            raw = _raw(case, page)
            if not raw.is_file():
                self.skipTest(f"missing {raw}")
            self.assertFalse(is_blank_page(raw), f"{case} p{page}")

    def test_diluted_slip_on_open_bed_is_not_blank(self) -> None:
        """Small CARDNET slip on a full-bed paper ROI (gin page 3)."""
        raw = _raw("gin", 3)
        if not raw.is_file():
            self.skipTest(f"missing {raw}")
        self.assertFalse(is_blank_page(raw), "gin p3")

    def test_gin_scrap_back_stays_blank(self) -> None:
        raw = _raw("gin", 4)
        if not raw.is_file():
            self.skipTest(f"missing {raw}")
        self.assertTrue(is_blank_page(raw), "gin p4")


class PartitionBlankPagesTests(unittest.TestCase):
    def test_single_page_uses_page_level_blank(self) -> None:
        alone = Path("page-0003.tiff")
        with patch(
            "yokaze.lib.brod.blanks.is_blank_page", return_value=True
        ):
            content, blank = partition_blank_pages([alone])
        self.assertEqual(content, [])
        self.assertEqual(blank, [alone])

    def test_all_blank_sheet_kept_as_content(self) -> None:
        """Duplex pair that both look blank — same as send, wipe neither."""
        a, b = Path("page-0003.tiff"), Path("page-0004.tiff")
        with patch(
            "yokaze.lib.brod.blanks.is_blank_page", return_value=True
        ):
            content, blank = partition_blank_pages([a, b])
        self.assertEqual(content, [a, b])
        self.assertEqual(blank, [])

    def test_mixed_batch_marks_only_blank_sides(self) -> None:
        front, back = Path("page-0001.tiff"), Path("page-0002.tiff")

        def fake_blank(path: Path) -> bool:
            return path == back

        with patch(
            "yokaze.lib.brod.blanks.is_blank_page", side_effect=fake_blank
        ):
            content, blank = partition_blank_pages([front, back])
        self.assertEqual(content, [front])
        self.assertEqual(blank, [back])


if __name__ == "__main__":
    unittest.main()
