"""Yokaze GTK app — scan, history, and crop lab."""

from __future__ import annotations

from pathlib import Path

from yokaze.lib import paths as _paths

# Reverse-DNS id required by GApplication and GNOME taskbar matching.
# Re-read via refresh_paths() / app_id() after identity env is set.
APP_ID = _paths.app_id()

ASSETS = Path(__file__).resolve().parent / "assets"

ROOT = _paths.find_repo_root()
DATA = _paths.resolve_data_dir()
CORPUS = DATA / "corpus"
# Crop-attempt bookmarks retired; crops live in metadata.history + attempts.json
ATTEMPTS_JSON = DATA / "attempts.json"
ARCHIVES = _paths.resolve_archives_dir()
CACHE = _paths.cache_dir()
LIB_CORE = ROOT / "lib" / "core"
LIB_CORPUS = LIB_CORE / "corpus"


def refresh_paths() -> None:
    """Re-bind module path globals after Settings override or identity change."""
    global APP_ID, DATA, CORPUS, ATTEMPTS_JSON, ARCHIVES, CACHE
    APP_ID = _paths.app_id()
    DATA = _paths.resolve_data_dir()
    CORPUS = DATA / "corpus"
    ATTEMPTS_JSON = DATA / "attempts.json"
    ARCHIVES = _paths.resolve_archives_dir()
    CACHE = _paths.cache_dir()
    # Keep backend's imported aliases in sync (tests monkeypatch those names).
    from yokaze.ui import backend as _backend

    _backend.ARCHIVES = ARCHIVES
    _backend.CORPUS = CORPUS
    _backend.ROOT = ROOT
