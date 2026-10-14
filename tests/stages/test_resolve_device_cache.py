"""Auto-detect must not re-run scanimage -L on every capture."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from yokaze.lib.capture import device as device_mod


class ResolveDeviceCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("SCANNER_DEVICE", None)
        device_mod.clear_device_cache()

    def tearDown(self) -> None:
        device_mod.clear_device_cache()
        os.environ.pop("SCANNER_DEVICE", None)

    def test_resolve_device_caches_auto_detect(self) -> None:
        with patch.object(
            device_mod,
            "refresh_device",
            side_effect=["fujitsu:ScanSnap iX1300:1", "fujitsu:ScanSnap iX1300:2"],
        ) as refresh:
            self.assertEqual(
                device_mod.resolve_device(), "fujitsu:ScanSnap iX1300:1"
            )
            self.assertEqual(
                device_mod.resolve_device(), "fujitsu:ScanSnap iX1300:1"
            )
            self.assertEqual(refresh.call_count, 1)

    def test_refresh_device_updates_cache(self) -> None:
        with patch.object(device_mod.subprocess, "run") as run:
            run.return_value.stdout = (
                "device `fujitsu:ScanSnap iX1300:99' is a "
                "FUJITSU ScanSnap iX1300 scanner\n"
            )
            run.return_value.stderr = ""
            first = device_mod.refresh_device()
            self.assertEqual(first, "fujitsu:ScanSnap iX1300:99")
            self.assertEqual(device_mod.resolve_device(), first)
            self.assertEqual(run.call_count, 1)

    def test_configured_device_bypasses_cache_and_refresh(self) -> None:
        os.environ["SCANNER_DEVICE"] = "fujitsu:ScanSnap iX1300:pinned"
        with patch.object(device_mod, "refresh_device") as refresh:
            self.assertEqual(
                device_mod.resolve_device(), "fujitsu:ScanSnap iX1300:pinned"
            )
            refresh.assert_not_called()

    def test_seed_device_cache_skips_refresh(self) -> None:
        device_mod.seed_device_cache("fujitsu:ScanSnap iX1300:seeded")
        with patch.object(device_mod, "refresh_device") as refresh:
            self.assertEqual(
                device_mod.resolve_device(), "fujitsu:ScanSnap iX1300:seeded"
            )
            refresh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
