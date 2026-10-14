"""scanimage failure lines must surface jam / sane_start messages."""

from __future__ import annotations

import unittest

from yokaze.lib.capture.device import _scanimage_error_line


class ScanimageErrorLineTests(unittest.TestCase):
    def test_surfaces_document_feeder_jammed(self) -> None:
        output = (
            "Scanning page 1\n"
            "scanimage: sane_start: Document feeder jammed\n"
            "Batch terminated, 0 pages scanned\n"
        )
        self.assertEqual(
            _scanimage_error_line(output),
            "sane_start: Document feeder jammed",
        )

    def test_surfaces_open_failed(self) -> None:
        output = "scanimage: open of device foo failed: Invalid argument\n"
        self.assertIn("failed", _scanimage_error_line(output).lower())


if __name__ == "__main__":
    unittest.main()
