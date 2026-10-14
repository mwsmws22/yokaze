"""Staging tray: append, reorder, overlay paint (no Paperless)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from yokaze.lib.metadata import record_history
from yokaze.lib.util.types import CropRect as LibCropRect
from yokaze.ui.deliveries import Delivery, DeliveryPage
from yokaze.ui.models import CropRect
from yokaze.ui.staging_tray import (
    StagingTray,
    StagedPage,
    block_insert_index,
    render_staged_page_bgr,
    restage_delivery,
)


def _write_tiff(path: Path, w: int = 40, h: int = 30, color=(0, 128, 255)) -> None:
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:] = color
    assert cv2.imwrite(str(path), img)


class StagingTrayTest(unittest.TestCase):
    def test_append_workdir_copies_pages_and_crops(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = Path(raw) / "work"
            work.mkdir()
            a = work / "page-0001.tiff"
            b = work / "page-0002.tiff"
            _write_tiff(a, 40, 30)
            _write_tiff(b, 40, 30, color=(10, 20, 30))

            tray = StagingTray()
            crops = {
                a.resolve(): LibCropRect(5, 5, 20, 15),
                b.resolve(): None,
            }
            added = tray.append_workdir(
                work, color_mode="color", scan_name="scan-test", page_crops=crops
            )
            self.assertEqual(added, 2)
            self.assertEqual(len(tray), 2)
            self.assertEqual(tray.pages[0].crop, CropRect(5, 5, 20, 15))
            self.assertEqual(tray.pages[1].crop, CropRect(0, 0, 40, 30))
            self.assertNotEqual(tray.pages[0].path, a)
            self.assertTrue(tray.pages[0].path.is_file())
            self.assertFalse(tray.pages[0].finished)
            tray.clear()

    def test_move_and_remove_pages(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = Path(raw) / "work"
            work.mkdir()
            for i in range(1, 4):
                _write_tiff(work / f"page-{i:04d}.tiff")

            tray = StagingTray()
            tray.append_workdir(work, color_mode="gray", scan_name="s")
            ids = [p.id for p in tray.pages]
            tray.move(ids[0], delta=1)
            self.assertEqual([p.id for p in tray.pages], [ids[1], ids[0], ids[2]])
            tray.nudge_block([ids[1], ids[2]], 1)
            self.assertEqual([p.id for p in tray.pages], [ids[0], ids[1], ids[2]])
            tray.place_block([ids[0], ids[2]], 0)
            self.assertEqual([p.id for p in tray.pages], [ids[0], ids[2], ids[1]])
            self.assertEqual(
                block_insert_index(
                    [ids[0], ids[2], ids[1]],
                    [ids[0], ids[2]],
                    ids[1],
                    after=True,
                ),
                1,
            )
            tray.remove(ids[1])
            self.assertEqual(len(tray), 2)
            self.assertNotIn(ids[1], {p.id for p in tray.pages})
            tray.clear()

    def test_undo_delete_restores_page_at_index(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = Path(raw) / "work"
            work.mkdir()
            for i in range(1, 4):
                _write_tiff(work / f"page-{i:04d}.tiff")

            tray = StagingTray()
            tray.append_workdir(work, color_mode="color", scan_name="s")
            ids = [p.id for p in tray.pages]
            path = tray.pages[1].path
            tray.remove(ids[1])
            self.assertEqual([p.id for p in tray.pages], [ids[0], ids[2]])
            self.assertTrue(path.is_file())
            restored = tray.undo_delete()
            self.assertEqual(restored, ids[1])
            self.assertEqual([p.id for p in tray.pages], ids)
            self.assertEqual(tray.selected_id, ids[1])
            self.assertIsNone(tray.undo_delete())
            tray.clear()

    def test_render_staged_page_applies_crop_and_rotate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "page.tiff"
            img = np.zeros((20, 30, 3), dtype=np.uint8)
            img[0:10, 0:15] = (0, 0, 255)  # red (BGR)
            img[0:10, 15:30] = (0, 255, 0)
            img[10:20, 0:15] = (255, 0, 0)
            img[10:20, 15:30] = (255, 255, 255)
            cv2.imwrite(str(path), img)

            page = StagedPage(
                id="1",
                path=path,
                crop=CropRect(0, 0, 15, 10),
                rotate=90,
                image_w=30,
                image_h=20,
            )
            out = render_staged_page_bgr(page)
            # Crop 15×10 (w×h) then CW 90 → 10×15 (w×h) → shape (15, 10)
            self.assertEqual(out.shape[0], 15)
            self.assertEqual(out.shape[1], 10)
            # Top-left red of crop lands at top-right after CW90
            self.assertEqual(int(out[0, 9, 2]), 255)

    def test_clear_removes_temp_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work = Path(raw) / "work"
            work.mkdir()
            _write_tiff(work / "page-0001.tiff")
            tray = StagingTray()
            tray.append_workdir(work, color_mode="color", scan_name="s")
            root = tray._root
            self.assertIsNotNone(root)
            assert root is not None
            self.assertTrue(root.is_dir())
            tray.clear()
            self.assertEqual(len(tray), 0)
            self.assertTrue(root is None or not root.is_dir())

    def test_restage_replays_history_ignores_brod_pin(self) -> None:
        """Restage tip-replays; does not require delivery brod_pin == current."""
        with tempfile.TemporaryDirectory() as tmp:
            archives = Path(tmp) / "archives"
            name = "scan-old"
            tree = archives / name
            raw_dir = tree / "raw"
            raw_dir.mkdir(parents=True)
            img = np.zeros((40, 60, 3), dtype=np.uint8)
            img[:] = (40, 40, 40)
            img[5:35, 10:50] = (200, 200, 200)
            cv2.imwrite(str(raw_dir / "page-0001.tiff"), img)
            record_history(
                tree,
                1,
                attempt=0,
                deskew_deg=0.0,
                rotate=0,
                rough={"x": 0, "y": 0, "w": 60, "h": 40},
            )
            delivery = Delivery(
                task_uuid="uuid-old",
                brod_pin="deadbeef" * 5,  # deliberately ≠ any current pin
                pages=[
                    DeliveryPage(
                        archive=name,
                        page=1,
                        crop=CropRect(10, 5, 40, 30),
                        rotate=0,
                    )
                ],
                name=name,
            )
            import yokaze.ui as ui

            prev = ui.ARCHIVES
            ui.ARCHIVES = archives
            try:
                tray = StagingTray()
                added = restage_delivery(delivery, tray)
                self.assertEqual(added, 1)
                self.assertEqual(tray.pages[0].crop, CropRect(10, 5, 40, 30))
                self.assertEqual(tray.pages[0].archive, name)
                self.assertEqual(tray.replace_pending, ["uuid-old"])
                self.assertFalse(tray.pages[0].finished)
                tray.clear()
            finally:
                ui.ARCHIVES = prev

    def test_durable_persist_and_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "stage-tray"
            root.mkdir()
            work = Path(raw) / "work"
            work.mkdir()
            _write_tiff(work / "page-0001.tiff", 40, 30)

            tray = StagingTray(durable=True, _root=root)
            tray.append_workdir(work, color_mode="color", scan_name="s")
            src = tray.pages[0]
            src.finished = True
            id_a, id_b = tray.replace_with_halves(
                src.id, CropRect(0, 0, 40, 15), CropRect(0, 15, 40, 15)
            )
            tray.selected_id = id_b
            tray.replace_pending.append("pending-1")
            tray.persist()

            # Simulate reopen via the same on-disk root.
            import yokaze.ui.staging_tray as st

            prev = st._default_stage_dir
            st._default_stage_dir = lambda: root  # type: ignore[method-assign]
            try:
                restored = StagingTray.load_persisted()
            finally:
                st._default_stage_dir = prev  # type: ignore[method-assign]

            self.assertTrue(restored.durable)
            self.assertEqual(len(restored), 2)
            self.assertEqual([p.id for p in restored.pages], [id_a, id_b])
            self.assertEqual(restored.pages[0].crop, CropRect(0, 0, 40, 15))
            self.assertEqual(restored.pages[1].crop, CropRect(0, 15, 40, 15))
            self.assertEqual(restored.pages[0].path, restored.pages[1].path)
            self.assertTrue(restored.pages[0].path.is_file())
            self.assertEqual(restored.replace_pending, ["pending-1"])
            self.assertEqual(restored.selected_id, id_b)
            self.assertTrue(restored.pages[0].finished)
            self.assertTrue(restored.pages[1].finished)
            restored.clear()


if __name__ == "__main__":
    unittest.main()
