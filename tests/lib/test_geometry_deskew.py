from yokaze.lib.geometry import CropRect, project_crop, round_deskew


def test_lib_geometry_round_deskew() -> None:
    assert round_deskew(1.03) == 1.05


def test_lib_geometry_project_identity() -> None:
    crop = CropRect(10, 20, 30, 40)
    assert project_crop(crop, 0.0, 200, 200) == crop
