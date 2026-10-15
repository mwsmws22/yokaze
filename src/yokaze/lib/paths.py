"""Identity-scoped XDG paths; history override separate from lab data."""

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


def app_id_for(ident: str) -> str:
    if ident not in _VALID_IDENTITIES:
        ident = IDENTITY_USER
    return _APP_IDS[ident]


def app_id() -> str:
    return app_id_for(identity())


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


def _read_app_conf() -> dict[str, str]:
    path = app_conf_path()
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def _write_app_conf(values: dict[str, str]) -> None:
    conf = app_conf_path()
    conf.parent.mkdir(parents=True, exist_ok=True)
    if not values:
        if conf.is_file():
            conf.unlink()
        return
    lines = ["# Yokaze app settings"]
    for key in sorted(values):
        lines.append(f"{key}={values[key]}")
    conf.write_text("\n".join(lines) + "\n")


def load_history_dir_override() -> Path | None:
    """Settings override for the History root (directory of archive slug folders)."""
    conf = _read_app_conf()
    for key in ("history_dir", "archives_dir"):
        raw = conf.get(key, "").strip()
        if raw:
            return Path(raw).expanduser()
    # Legacy: data_dir was the parent of the history/archives root.
    legacy = conf.get("data_dir", "").strip()
    if legacy:
        return Path(legacy).expanduser() / "history"
    return None


def save_history_dir_override(history_dir: Path | None) -> None:
    """Persist History override, or clear it (XDG default). Drops legacy keys."""
    conf = _read_app_conf()
    conf.pop("data_dir", None)
    conf.pop("archives_dir", None)
    if history_dir is None:
        conf.pop("history_dir", None)
    else:
        conf["history_dir"] = str(history_dir.expanduser())
    _write_app_conf(conf)


# Back-compat aliases (folder was formerly called archives).
load_archives_dir_override = load_history_dir_override
save_archives_dir_override = save_history_dir_override


def load_data_dir_override() -> Path | None:
    """Deprecated: returns parent of history override when set."""
    history = load_history_dir_override()
    return None if history is None else history.parent


def save_data_dir_override(data_dir: Path | None) -> None:
    """Deprecated: Settings used to store the parent of history/."""
    if data_dir is None:
        save_history_dir_override(None)
        return
    save_history_dir_override(Path(data_dir).expanduser() / "history")


def resolve_history_dir() -> Path:
    """Scan History root — XDG by default; Settings may override."""
    override = load_history_dir_override()
    if override is not None:
        return override
    return data_xdg_dir() / "history"


resolve_archives_dir = resolve_history_dir


def resolve_stage_tray_dir() -> Path:
    """Durable Stage tab tray (survives quit / F5)."""
    return data_xdg_dir() / "stage-tray"


def resolve_deliveries_path() -> Path:
    """Local Paperless delivery index (UUID → pages for Restage)."""
    return data_xdg_dir() / "deliveries.json"


def resolve_delivery_brod_cache_dir() -> Path:
    """Pin-keyed Restage re-brod TIFF cache."""
    return cache_dir() / "delivery-brod"


def resolve_data_dir() -> Path:
    """Lab / workbench data root (corpus, attempts, pins, …).

    Dev always uses ``<repo>/data``. User (release) uses the XDG data dir.
    History, Stage tray, and delivery-brod cache are resolved separately under XDG.
    """
    if identity() == IDENTITY_DEV:
        return find_repo_root() / "data"
    return data_xdg_dir()
