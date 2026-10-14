"""ADF paper size: Auto (ALD + 3 m ceiling) vs fixed A4 + bleed."""

from __future__ import annotations

import os
import unittest

from yokaze.lib.capture import device as device_mod


class LongPageScanBatchCmdTests(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("LONG_PAGE", None)
        os.environ.pop("PAPER_SIZE", None)

    def test_normal_adf_uses_a4_bleed_without_ald(self) -> None:
        cmd = device_mod.scan_batch_cmd(
            "fujitsu:ScanSnap iX1300:1",
            "ADF Duplex",
            "/tmp/batch-page",
            long_page=False,
        )
        self.assertIn("--page-height", cmd)
        height = cmd[cmd.index("--page-height") + 1]
        self.assertEqual(height, str(device_mod.PAGE_HEIGHT_MM))
        self.assertNotIn("--ald", cmd)

    def test_long_page_adf_raises_height_and_enables_ald(self) -> None:
        cmd = device_mod.scan_batch_cmd(
            "fujitsu:ScanSnap iX1300:1",
            "ADF Duplex",
            "/tmp/batch-page",
            long_page=True,
        )
        height = cmd[cmd.index("--page-height") + 1]
        self.assertEqual(height, str(device_mod.LONG_PAGE_HEIGHT_MM))
        self.assertGreater(device_mod.LONG_PAGE_HEIGHT_MM, device_mod.PAGE_HEIGHT_MM)
        self.assertIn("--ald=yes", cmd)
        self.assertNotIn("--ald", [a for a in cmd if a != "--ald=yes"])

    def test_long_page_ignored_for_card(self) -> None:
        cmd = device_mod.scan_batch_cmd(
            "fujitsu:ScanSnap iX1300:1",
            "Card Duplex",
            "/tmp/batch-page",
            long_page=True,
        )
        height = cmd[cmd.index("--page-height") + 1]
        self.assertEqual(height, str(device_mod.PAGE_HEIGHT_MM))
        self.assertNotIn("--ald", cmd)

    def test_long_page_enabled_defaults_to_auto(self) -> None:
        os.environ.pop("LONG_PAGE", None)
        os.environ.pop("PAPER_SIZE", None)
        self.assertTrue(device_mod.long_page_enabled())

    def test_paper_size_a4_disables_long_page(self) -> None:
        os.environ.pop("LONG_PAGE", None)
        os.environ["PAPER_SIZE"] = "a4"
        self.assertFalse(device_mod.long_page_enabled())

    def test_paper_size_auto_enables_long_page(self) -> None:
        os.environ.pop("LONG_PAGE", None)
        os.environ["PAPER_SIZE"] = "auto"
        self.assertTrue(device_mod.long_page_enabled())

    def test_long_page_env_overrides_paper_size(self) -> None:
        os.environ["PAPER_SIZE"] = "a4"
        os.environ["LONG_PAGE"] = "1"
        self.assertTrue(device_mod.long_page_enabled())
        os.environ["LONG_PAGE"] = "0"
        os.environ["PAPER_SIZE"] = "auto"
        self.assertFalse(device_mod.long_page_enabled())


if __name__ == "__main__":
    unittest.main()
