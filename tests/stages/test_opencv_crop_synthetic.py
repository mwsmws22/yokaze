from pathlib import Path
import numpy as np
import cv2
from yokaze.lib.util.types import PageWork
from yokaze.lib.crop.opencv_crop import OpenCvCrop

def test_detects_dark_rect_on_light_bed(tmp_path: Path):
    img = np.full((400, 300, 3), 200, dtype=np.uint8)  # light bed
    img[80:320, 40:260] = 40  # dark paper
    path = tmp_path / "page.tiff"
    cv2.imwrite(str(path), img)
    page = PageWork(path=path, page_num=1)
    out = OpenCvCrop(margin_px=2).crop_page(page)
    assert out.crop is not None
    assert 30 <= out.crop.x <= 50
    assert 70 <= out.crop.y <= 90
    assert out.crop.w > 150 and out.crop.h > 150
