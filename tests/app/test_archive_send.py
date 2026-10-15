"""History archive send: prepare, stage append, Paperless (mocked upload)."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import cv2
import numpy as np
import pytest

from yokaze.lib.util.types import CropRect as LibCropRect
from yokaze.ui.staging_tray import StagingTray


def _write_tiff(path: Path, *, blankish: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if blankish:
        img = np.full((200, 150, 3), 230, dtype=np.uint8)
    else:
        img = np.full((400, 300, 3), 200, dtype=np.uint8)
        img[40:360, 50:250] = 240
        img[80:320, 80:220] = 40
    assert cv2.imwrite(str(path), img)


def test_prepare_archive_pages_drops_blank_raws(tmp_path, monkeypatch):
    import yokaze.ui as ui
    import yokaze.ui.archive_send as archive_send

    archives = tmp_path / "archives"
    raw = archives / "scan-a" / "raw"
    _write_tiff(raw / "page-0001.tiff", blankish=False)
    _write_tiff(raw / "page-0002.tiff", blankish=True)
    monkeypatch.setattr(ui, "ARCHIVES", archives)

    def fake_blank(path: Path) -> bool:
        return path.name.endswith("0002.tiff")

    def fake_rebord(archive: str, workdir: Path):
        from yokaze.lib.util.types import PageWork

        assert archive == "scan-a"
        src = archives / archive / "raw" / "page-0001.tiff"
        dest = workdir / "page-0001.tiff"
        dest.write_bytes(src.read_bytes())
        return [PageWork(path=dest, page_num=1)]

    monkeypatch.setattr(archive_send, "is_blank_page", fake_blank)
    monkeypatch.setattr(archive_send, "rebord_archive_to_workdir", fake_rebord)

    with mock.patch.object(
        archive_send,
        "_detect_crops",
        return_value={Path("x"): LibCropRect(1, 2, 3, 4)},
    ):
        # prepare will remap detect by actual paths — stub detect after rebord
        def detect(workdir: Path):
            p = next(workdir.glob("page-*.tiff"))
            return {p.resolve(): LibCropRect(5, 6, 10, 12)}

        monkeypatch.setattr(archive_send, "_detect_crops", detect)
        prepared = archive_send.prepare_archive_pages("scan-a")

    try:
        pages = sorted(prepared.workdir.glob("page-*.tiff"))
        assert len(pages) == 1
        assert pages[0].name == "page-0001.tiff"
        assert prepared.archive_pages[pages[0].resolve()] == 1
        assert prepared.crops[pages[0].resolve()] == LibCropRect(5, 6, 10, 12)
    finally:
        prepared.cleanup()


def test_stage_archive_appends_to_tray(tmp_path, monkeypatch):
    import yokaze.ui as ui
    import yokaze.ui.archive_send as archive_send

    archives = tmp_path / "archives"
    raw = archives / "scan-b" / "raw"
    _write_tiff(raw / "page-0001.tiff")
    monkeypatch.setattr(ui, "ARCHIVES", archives)

    def fake_blank(_path: Path) -> bool:
        return False

    def fake_rebord(archive: str, workdir: Path):
        from yokaze.lib.util.types import PageWork

        src = archives / archive / "raw" / "page-0001.tiff"
        dest = workdir / "page-0001.tiff"
        dest.write_bytes(src.read_bytes())
        return [PageWork(path=dest, page_num=1)]

    monkeypatch.setattr(archive_send, "is_blank_page", fake_blank)
    monkeypatch.setattr(archive_send, "rebord_archive_to_workdir", fake_rebord)

    def detect(workdir: Path):
        p = next(workdir.glob("page-*.tiff"))
        return {p.resolve(): LibCropRect(2, 3, 20, 15)}

    monkeypatch.setattr(archive_send, "_detect_crops", detect)

    tray = StagingTray()
    try:
        added = archive_send.stage_archive("scan-b", tray, mode="reprocess")
        assert added == 1
        assert len(tray) == 1
        assert tray.pages[0].archive == "scan-b"
        assert tray.pages[0].page == 1
        assert tray.pages[0].crop.x == 2
    finally:
        tray.clear()


def test_rebord_does_not_delete_archive_blank_raws(tmp_path, monkeypatch):
    """Regression: never call drop_blank_pages on archive raws (it unlinks)."""
    import yokaze.ui as ui
    import yokaze.ui.archive_send as archive_send

    archives = tmp_path / "archives"
    raw = archives / "scan-keep" / "raw"
    _write_tiff(raw / "page-0001.tiff", blankish=False)
    blank = raw / "page-0002.tiff"
    _write_tiff(blank, blankish=True)
    monkeypatch.setattr(ui, "ARCHIVES", archives)

    monkeypatch.setattr(
        archive_send, "is_blank_page", lambda p: p.name.endswith("0002.tiff")
    )

    def fake_preprocess(works, **_kwargs):
        return works

    class FakePipeline:
        class preprocess:
            preprocess_pages = staticmethod(fake_preprocess)

    monkeypatch.setattr(
        "yokaze.pipeline.wiring.default_pipeline", lambda: FakePipeline()
    )
    monkeypatch.setattr(
        "yokaze.lib.brod.rotation_plan.resolve_rotation_plan",
        lambda paths, scan_source=None: [0] * len(paths),
    )
    monkeypatch.setattr(
        "yokaze.lib.brod.rotation_plan.resolve_scan_source", lambda: "ADF Duplex"
    )

    work = tmp_path / "work"
    works = archive_send.rebord_archive_to_workdir("scan-keep", work)
    assert [w.page_num for w in works] == [1]
    assert blank.is_file(), "archive blank raw must not be deleted"
    assert (raw / "page-0001.tiff").is_file()


def test_send_archive_to_paperless_gates_before_prepare(tmp_path, monkeypatch):
    """Dirty-tree / pin gates must run before rebord work."""
    import yokaze.ui.archive_send as archive_send

    calls: list[str] = []

    def fake_gates(*, action: str = "scan") -> None:
        calls.append(f"gates:{action}")
        raise RuntimeError("refusing Send to Paperless: working tree is dirty")

    def boom_stage(*_a, **_k):
        calls.append("stage")
        raise AssertionError("stage_archive must not run after gate failure")

    monkeypatch.setattr(archive_send, "require_paperless_send_gates", fake_gates)
    monkeypatch.setattr(archive_send, "stage_archive", boom_stage)

    with pytest.raises(RuntimeError, match="working tree is dirty"):
        archive_send.send_archive_to_paperless("scan-x")
    assert calls == ["gates:Send to Paperless"]


def test_send_archive_to_paperless_delegates_to_send_staging_tray(
    tmp_path, monkeypatch
):
    import yokaze.ui as ui
    import yokaze.ui.archive_send as archive_send

    archives = tmp_path / "archives"
    raw = archives / "scan-c" / "raw"
    _write_tiff(raw / "page-0001.tiff")
    monkeypatch.setattr(ui, "ARCHIVES", archives)

    monkeypatch.setattr(archive_send, "is_blank_page", lambda _p: False)
    monkeypatch.setattr(
        archive_send, "require_paperless_send_gates", lambda **_k: None
    )

    def fake_rebord(archive: str, workdir: Path):
        from yokaze.lib.util.types import PageWork

        src = archives / archive / "raw" / "page-0001.tiff"
        dest = workdir / "page-0001.tiff"
        dest.write_bytes(src.read_bytes())
        return [PageWork(path=dest, page_num=1)]

    monkeypatch.setattr(archive_send, "rebord_archive_to_workdir", fake_rebord)
    monkeypatch.setattr(
        archive_send,
        "_detect_crops",
        lambda workdir: {
            next(workdir.glob("page-*.tiff")).resolve(): LibCropRect(0, 0, 10, 10)
        },
    )

    called: dict = {}

    def fake_send(
        tray: StagingTray,
        *,
        name: str | None = None,
        target: str = "paperless",
        **_kw,
    ) -> str:
        called["n"] = len(tray.pages)
        called["name"] = name
        called["target"] = target
        assert tray.replace_pending == []
        return "Done: uploaded"

    monkeypatch.setattr(archive_send, "send_staging_tray", fake_send)
    msg = archive_send.send_archive_to_paperless("scan-c", mode="reprocess")
    assert msg == "Done: uploaded"
    assert called == {"n": 1, "name": "scan-c", "target": "paperless"}


def test_prepare_archive_from_history_uses_tip(tmp_path, monkeypatch):
    import json

    import yokaze.ui as ui
    import yokaze.ui.archive_send as archive_send
    from yokaze.lib.metadata import page_key

    archives = tmp_path / "archives"
    tree = archives / "scan-tip"
    raw = tree / "raw"
    _write_tiff(raw / "page-0001.tiff")
    meta = {
        "pages": {
            page_key(1): {
                "history": [
                    {
                        "rough": {"x": 1, "y": 2, "w": 100, "h": 80},
                        "rotate": 0,
                        "deskew": 0.0,
                        "crop": {"x": 4, "y": 5, "w": 40, "h": 30},
                    }
                ]
            }
        }
    }
    (tree / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    monkeypatch.setattr(ui, "ARCHIVES", archives)

    fake_brod = np.full((80, 100, 3), 90, dtype=np.uint8)

    monkeypatch.setattr(
        "yokaze.ui.archive_thumbs.replay_archive_brod_bgr",
        lambda archive, page, **_k: fake_brod,
    )
    monkeypatch.setattr(
        "yokaze.ui.backend.list_archive_pages",
        lambda name: [1],
    )
    monkeypatch.setattr(
        "yokaze.ui.deliveries.load_deliveries",
        lambda path=None: [],
    )

    prepared = archive_send.prepare_archive_from_history("scan-tip")
    try:
        p = next(prepared.workdir.glob("page-0001.tiff"))
        assert prepared.archive_pages[p.resolve()] == 1
        assert prepared.crops[p.resolve()] == LibCropRect(4, 5, 40, 30)
    finally:
        prepared.cleanup()


def test_prepare_archive_from_history_prefers_delivery_crop(tmp_path, monkeypatch):
    import json

    import yokaze.ui as ui
    import yokaze.ui.archive_send as archive_send
    from yokaze.lib.metadata import page_key
    from yokaze.ui.deliveries import Delivery, DeliveryPage
    from yokaze.ui.models import CropRect

    archives = tmp_path / "archives"
    tree = archives / "scan-del"
    raw = tree / "raw"
    _write_tiff(raw / "page-0001.tiff")
    meta = {
        "pages": {
            page_key(1): {
                "history": [
                    {
                        "rough": {"x": 1, "y": 2, "w": 100, "h": 80},
                        "rotate": 0,
                        "deskew": 0.0,
                    }
                ]
            }
        }
    }
    (tree / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    monkeypatch.setattr(ui, "ARCHIVES", archives)
    monkeypatch.setattr(
        "yokaze.ui.archive_thumbs.replay_archive_brod_bgr",
        lambda archive, page, **_k: np.full((80, 100, 3), 90, dtype=np.uint8),
    )
    monkeypatch.setattr(
        "yokaze.ui.backend.list_archive_pages",
        lambda name: [1],
    )
    monkeypatch.setattr(
        "yokaze.ui.deliveries.load_deliveries",
        lambda path=None: [
            Delivery(
                task_uuid="u1",
                brod_pin="x",
                pages=[
                    DeliveryPage(
                        archive="scan-del",
                        page=1,
                        crop=CropRect(10, 20, 30, 40),
                        rotate=90,
                    )
                ],
            )
        ],
    )

    prepared = archive_send.prepare_archive_from_history("scan-del")
    try:
        p = next(prepared.workdir.glob("page-0001.tiff"))
        assert prepared.crops[p.resolve()] == LibCropRect(10, 20, 30, 40)
        assert prepared.rotates[p.resolve()] == 90
    finally:
        prepared.cleanup()


def test_prepare_archive_from_history_detects_when_no_saved_crop(
    tmp_path, monkeypatch
):
    import json

    import yokaze.ui as ui
    import yokaze.ui.archive_send as archive_send
    from yokaze.lib.metadata import page_key

    archives = tmp_path / "archives"
    tree = archives / "scan-detect"
    raw = tree / "raw"
    _write_tiff(raw / "page-0001.tiff")
    meta = {
        "pages": {
            page_key(1): {
                "history": [
                    {
                        "rough": {"x": 1, "y": 2, "w": 100, "h": 80},
                        "rotate": 0,
                        "deskew": 0.0,
                    }
                ]
            }
        }
    }
    (tree / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    monkeypatch.setattr(ui, "ARCHIVES", archives)
    monkeypatch.setattr(
        "yokaze.ui.archive_thumbs.replay_archive_brod_bgr",
        lambda archive, page, **_k: np.full((80, 100, 3), 90, dtype=np.uint8),
    )
    monkeypatch.setattr(
        "yokaze.ui.backend.list_archive_pages",
        lambda name: [1],
    )
    monkeypatch.setattr(
        "yokaze.ui.deliveries.load_deliveries",
        lambda path=None: [],
    )
    monkeypatch.setattr(
        archive_send,
        "_detect_crops",
        lambda workdir: {
            next(workdir.glob("page-*.tiff")).resolve(): LibCropRect(7, 8, 9, 10)
        },
    )

    prepared = archive_send.prepare_archive_from_history("scan-detect")
    try:
        p = next(prepared.workdir.glob("page-0001.tiff"))
        assert prepared.crops[p.resolve()] == LibCropRect(7, 8, 9, 10)
    finally:
        prepared.cleanup()


def test_prepare_archive_from_history_missing_tip_fails(tmp_path, monkeypatch):
    import yokaze.ui as ui
    import yokaze.ui.archive_send as archive_send

    archives = tmp_path / "archives"
    raw = archives / "scan-bare" / "raw"
    _write_tiff(raw / "page-0001.tiff")
    (archives / "scan-bare" / "metadata.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ui, "ARCHIVES", archives)
    monkeypatch.setattr(
        "yokaze.ui.backend.list_archive_pages",
        lambda name: [1],
    )

    with pytest.raises(RuntimeError, match="no archive history"):
        archive_send.prepare_archive_from_history("scan-bare")


def test_prepare_archive_from_history_skips_blank_back_without_history(
    tmp_path, monkeypatch
):
    import json

    import yokaze.ui as ui
    import yokaze.ui.archive_send as archive_send
    from yokaze.lib.metadata import page_key

    archives = tmp_path / "archives"
    tree = archives / "scan-duplex"
    raw = tree / "raw"
    _write_tiff(raw / "page-0001.tiff")
    _write_tiff(raw / "page-0002.tiff", blankish=True)
    meta = {
        "pages": {
            page_key(1): {
                "history": [
                    {
                        "rough": {"x": 1, "y": 2, "w": 100, "h": 80},
                        "rotate": 90,
                        "deskew": 0.0,
                        "crop": {"x": 4, "y": 5, "w": 40, "h": 30},
                    }
                ]
            }
        }
    }
    (tree / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    monkeypatch.setattr(ui, "ARCHIVES", archives)
    monkeypatch.setattr(
        "yokaze.ui.archive_thumbs.replay_archive_brod_bgr",
        lambda archive, page, **_k: np.full((80, 100, 3), 90, dtype=np.uint8),
    )
    monkeypatch.setattr(
        "yokaze.ui.backend.list_archive_pages",
        lambda name: [1, 2],
    )
    monkeypatch.setattr(
        "yokaze.ui.backend.archive_raw_path",
        lambda name, page: raw / f"page-{page:04d}.tiff",
    )
    monkeypatch.setattr(
        archive_send,
        "is_blank_page",
        lambda path: path.name.endswith("0002.tiff"),
    )
    monkeypatch.setattr(
        "yokaze.ui.deliveries.load_deliveries",
        lambda path=None: [],
    )

    prepared = archive_send.prepare_archive_from_history("scan-duplex")
    try:
        written = sorted(p.name for p in prepared.workdir.glob("page-*.tiff"))
        assert written == ["page-0001.tiff"]
    finally:
        prepared.cleanup()
