"""Identity-scoped XDG paths; history override separate from lab data."""

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
    assert paths.resolve_history_dir() == xdg_home / "data" / "yokaze" / "history"
    assert paths.resolve_archives_dir() == paths.resolve_history_dir()
    assert paths.resolve_stage_tray_dir() == xdg_home / "data" / "yokaze" / "stage-tray"
    assert paths.resolve_deliveries_path() == (
        xdg_home / "data" / "yokaze" / "deliveries.json"
    )
    assert paths.resolve_delivery_brod_cache_dir() == (
        xdg_home / "cache" / "yokaze" / "delivery-brod"
    )
    assert paths.cache_dir() == xdg_home / "cache" / "yokaze"
    assert paths.config_dir() == xdg_home / "config" / "yokaze"


def test_dev_lab_data_is_repo_history_is_xdg(
    xdg_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("YOKAZE_IDENTITY", "dev")
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname='yokaze'\n")
    monkeypatch.setattr(paths, "find_repo_root", lambda: repo)
    assert paths.app_id() == "com.github.yokaze.Yokaze.Dev"
    assert paths.resolve_data_dir() == repo / "data"
    assert paths.resolve_history_dir() == xdg_home / "data" / "yokaze-dev" / "history"
    assert paths.config_dir() == xdg_home / "config" / "yokaze-dev"


def test_history_override_wins(xdg_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOKAZE_IDENTITY", "user")
    override = xdg_home / "custom-history"
    paths.save_history_dir_override(override)
    assert paths.resolve_history_dir() == override
    assert paths.resolve_data_dir() == xdg_home / "data" / "yokaze"
    paths.save_history_dir_override(None)
    assert paths.resolve_history_dir() == xdg_home / "data" / "yokaze" / "history"


def test_legacy_archives_dir_key_still_read(
    xdg_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("YOKAZE_IDENTITY", "user")
    override = xdg_home / "old-archives-key"
    conf = paths.app_conf_path()
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text(f"# Yokaze app settings\narchives_dir={override}\n")
    assert paths.resolve_history_dir() == override


def test_legacy_data_dir_maps_to_history_subdir(
    xdg_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("YOKAZE_IDENTITY", "user")
    conf = paths.app_conf_path()
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text("# Yokaze app settings\ndata_dir=/old/data\n")
    assert paths.resolve_history_dir() == Path("/old/data/history")


def test_refresh_paths_updates_ui_globals(
    xdg_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("YOKAZE_IDENTITY", "user")
    import yokaze.ui as ui

    ui.refresh_paths()
    assert ui.APP_ID == "com.github.yokaze.Yokaze"
    assert ui.DATA == xdg_home / "data" / "yokaze"
    assert ui.HISTORY == xdg_home / "data" / "yokaze" / "history"
    assert ui.ARCHIVES == ui.HISTORY
    override = xdg_home / "other-history"
    paths.save_history_dir_override(override)
    ui.refresh_paths()
    assert ui.DATA == xdg_home / "data" / "yokaze"
    assert ui.HISTORY == override
    assert ui.ARCHIVES == override
