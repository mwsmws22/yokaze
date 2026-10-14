"""Identity-scoped XDG paths and Settings data-dir override."""

from __future__ import annotations

import os
from pathlib import Path

IDENTITY_USER = "user"
IDENTITY_DEV = "dev"
_VALID_IDENTITIES = frozenset({IDENTITY_USER, IDENTITY_DEV})

_APP_IDS = {
    IDENTITY_USER: "com.github.yokaze.Yokaze",
    IDENTITY_DEV: "com.github.yokaze.Yokaze.Dev",
}
_XDG_NAMES = {
    IDENTITY_USER: "yokaze",
    IDENTITY_DEV: "yokaze-dev",
}


def identity() -> str:
    raw = os.environ.get("YOKAZE_IDENTITY", IDENTITY_USER).strip().lower()
    if raw in _VALID_IDENTITIES:
        return raw
    return IDENTITY_USER


def app_id() -> str:
    return _APP_IDS[identity()]


def xdg_name() -> str:
    return _XDG_NAMES[identity()]


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / xdg_name()


def data_xdg_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / xdg_name()


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / xdg_name()


def app_conf_path() -> Path:
    return config_dir() / "app.conf"


def load_data_dir_override() -> Path | None:
    path = app_conf_path()
    if not path.is_file():
        return None
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() != "data_dir":
            continue
        raw = value.strip()
        if raw:
            return Path(raw).expanduser()
        return None
    return None


def save_data_dir_override(data_dir: Path | None) -> None:
    """Persist override, or clear it when ``data_dir`` is None (XDG default)."""
    conf = app_conf_path()
    conf.parent.mkdir(parents=True, exist_ok=True)
    if data_dir is None:
        if conf.is_file():
            conf.unlink()
        return
    conf.write_text(
        "# Yokaze app settings\n"
        f"data_dir={data_dir.expanduser()}\n"
    )


def resolve_data_dir() -> Path:
    override = load_data_dir_override()
    if override is not None:
        return override
    return data_xdg_dir()


def find_repo_root() -> Path:
    """Walk up from this package to the checkout that holds data/ (dev layout)."""
    here = Path(__file__).resolve().parent
    for parent in [here, *here.parents]:
        if (parent / "data").is_dir() and (
            (parent / "pyproject.toml").is_file() or (parent / "lib").is_dir()
        ):
            return parent
    # src/yokaze/lib → parents[2] is repo root in the standard layout
    return here.parents[2]
