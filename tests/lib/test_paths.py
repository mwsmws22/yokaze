"""Identity-scoped XDG paths and Settings data-dir override."""

from __future__ import annotations

from pathlib import Path

import pytest

from yokaze.lib import paths


@pytest.fixture()
def xdg_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "config"
    data = tmp_path / "data"
    cache = tmp_path / "cache"
    config.mkdir()
    data.mkdir()
    cache.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.delenv("YOKAZE_IDENTITY", raising=False)
    return tmp_path


def test_user_defaults_to_xdg(xdg_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOKAZE_IDENTITY", "user")
    assert paths.app_id() == "com.github.yokaze.Yokaze"
    assert paths.resolve_data_dir() == xdg_home / "data" / "yokaze"
    assert paths.cache_dir() == xdg_home / "cache" / "yokaze"
    assert paths.config_dir() == xdg_home / "config" / "yokaze"


def test_dev_defaults_to_xdg(xdg_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOKAZE_IDENTITY", "dev")
    assert paths.app_id() == "com.github.yokaze.Yokaze.Dev"
    assert paths.resolve_data_dir() == xdg_home / "data" / "yokaze-dev"
    assert paths.config_dir() == xdg_home / "config" / "yokaze-dev"


def test_settings_override_wins(xdg_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOKAZE_IDENTITY", "user")
    override = xdg_home / "custom-data"
    paths.save_data_dir_override(override)
    assert paths.resolve_data_dir() == override
    paths.save_data_dir_override(None)
    assert paths.resolve_data_dir() == xdg_home / "data" / "yokaze"


def test_refresh_paths_updates_ui_globals(
    xdg_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("YOKAZE_IDENTITY", "user")
    import yokaze.ui as ui

    ui.refresh_paths()
    assert ui.APP_ID == "com.github.yokaze.Yokaze"
    assert ui.DATA == xdg_home / "data" / "yokaze"
    override = xdg_home / "other"
    paths.save_data_dir_override(override)
    ui.refresh_paths()
    assert ui.DATA == override
    assert ui.ARCHIVES == override / "archives"
