"""Scanner and Paperless settings (identity-scoped scanner.conf)."""

from __future__ import annotations

import os
import re
import ssl
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from yokaze.lib import paths as app_paths

AUTO_DEVICE = ""
_SCANNER_RE = re.compile(r"device `([^']+)' is a")
_FUJITSU_RE = re.compile(r"fujitsu:ScanSnap")

DEFAULT_ENHANCE_WORKERS = 1

# ADF paper geometry. Auto = 3 m ceiling + ALD; A4 = fixed A4 + bleed.
PAPER_SIZE_AUTO = "auto"
PAPER_SIZE_A4 = "a4"
PAPER_SIZES = (PAPER_SIZE_AUTO, PAPER_SIZE_A4)
DEFAULT_PAPER_SIZE = PAPER_SIZE_AUTO

SEND_TARGET_PAPERLESS = "paperless"
SEND_TARGET_STAGE = "stage"
SEND_TARGET_NAPS2 = "naps2"
SEND_TARGET_FOLDER = "folder"
SEND_TARGETS = (
    SEND_TARGET_PAPERLESS,
    SEND_TARGET_STAGE,
    SEND_TARGET_NAPS2,
    SEND_TARGET_FOLDER,
)
DEFAULT_SEND_TARGETS = frozenset(
    {SEND_TARGET_PAPERLESS, SEND_TARGET_STAGE, SEND_TARGET_NAPS2}
)

_BUTTON_SERVICE = "yokaze-button.service"


def config_path() -> Path:
    """``~/.config/yokaze/scanner.conf`` or ``…/yokaze-dev/scanner.conf``."""
    return app_paths.config_dir() / "scanner.conf"


def _xdg_config_root() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return Path(base) if base else Path.home() / ".config"


def _legacy_config_paths() -> tuple[Path, ...]:
    env_d = _xdg_config_root() / "environment.d"
    return (env_d / "yokaze.conf", env_d / "scanner.conf")


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() in ("1", "true", "yes", "on")


def parse_paper_size(raw: str) -> str:
    value = raw.strip().lower()
    if value in PAPER_SIZES:
        return value
    return DEFAULT_PAPER_SIZE


@dataclass
class ScannerConfig:
    scanner_device: str = ""
    paperless_url: str = ""
    paperless_token: str = ""
    enhance_workers: int = DEFAULT_ENHANCE_WORKERS
    developer_mode: bool = False
    paper_size: str = DEFAULT_PAPER_SIZE
    send_targets: frozenset[str] = DEFAULT_SEND_TARGETS
    folder_dir: str = ""

    @property
    def is_configured(self) -> bool:
        return bool(self.paperless_url and self.paperless_token)


def _parse_enhance_workers(raw: str) -> int:
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_ENHANCE_WORKERS


def parse_send_targets(raw: str | None) -> frozenset[str]:
    """Comma-separated SEND_TARGETS; empty/missing → default Paperless+Stage+NAPS2."""
    if raw is None or not raw.strip():
        return DEFAULT_SEND_TARGETS
    found = {part.strip().lower() for part in raw.split(",") if part.strip()}
    valid = frozenset(t for t in SEND_TARGETS if t in found)
    return valid if valid else DEFAULT_SEND_TARGETS


def format_send_targets(targets: frozenset[str] | set[str]) -> str:
    return ",".join(t for t in SEND_TARGETS if t in targets)


def _parse_config_text(text: str) -> ScannerConfig:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()

    return ScannerConfig(
        scanner_device=values.get("SCANNER_DEVICE", ""),
        paperless_url=values.get("PAPERLESS_URL", ""),
        paperless_token=values.get("PAPERLESS_TOKEN", ""),
        enhance_workers=_parse_enhance_workers(
            values.get("SCAN_ENHANCE_WORKERS", str(DEFAULT_ENHANCE_WORKERS))
        ),
        developer_mode=_parse_bool(values.get("DEVELOPER_MODE", "0")),
        paper_size=parse_paper_size(values.get("PAPER_SIZE", DEFAULT_PAPER_SIZE)),
        send_targets=parse_send_targets(values.get("SEND_TARGETS")),
        folder_dir=values.get("FOLDER_DIR", "").strip(),
    )


def _maybe_migrate_legacy_user_conf(path: Path) -> None:
    """Copy old environment.d conf into user identity path once. Dev stays empty."""
    if app_paths.identity() != app_paths.IDENTITY_USER:
        return
    for legacy in _legacy_config_paths():
        if not legacy.is_file():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(legacy.read_text())
        return


def load_config() -> ScannerConfig:
    path = config_path()
    if not path.is_file():
        _maybe_migrate_legacy_user_conf(path)
    if not path.is_file():
        return ScannerConfig()
    return _parse_config_text(path.read_text())


def _config_file_text(cfg: ScannerConfig) -> str:
    paper_size = parse_paper_size(cfg.paper_size)
    lines = [
        "# Yokaze scanner / Paperless configuration",
        f"SCANNER_DEVICE={cfg.scanner_device}",
        f"PAPERLESS_URL={cfg.paperless_url}",
        f"PAPERLESS_TOKEN={cfg.paperless_token}",
        f"SCAN_ENHANCE_WORKERS={cfg.enhance_workers}",
        f"DEVELOPER_MODE={'1' if cfg.developer_mode else '0'}",
        f"PAPER_SIZE={paper_size}",
        f"SEND_TARGETS={format_send_targets(cfg.send_targets)}",
        f"FOLDER_DIR={cfg.folder_dir}",
    ]
    return "\n".join(lines) + "\n"


def save_config(cfg: ScannerConfig, *, restart_button: bool = True) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_config_file_text(cfg))
    apply_config(cfg, restart_button=restart_button)


def apply_config(cfg: ScannerConfig, *, restart_button: bool = False) -> None:
    # Set in *this* process too, so a GUI session picks up the new worker
    # count on the next scan document — no app restart needed.
    paper_size = parse_paper_size(cfg.paper_size)
    os.environ["SCANNER_DEVICE"] = cfg.scanner_device
    os.environ["PAPERLESS_URL"] = cfg.paperless_url
    os.environ["PAPERLESS_TOKEN"] = cfg.paperless_token
    os.environ["SCAN_ENHANCE_WORKERS"] = str(cfg.enhance_workers)
    os.environ["PAPER_SIZE"] = paper_size
    if cfg.folder_dir:
        os.environ["FOLDER_DIR"] = cfg.folder_dir
    else:
        os.environ.pop("FOLDER_DIR", None)

    if not restart_button:
        return
    subprocess.run(
        ["systemctl", "--user", "restart", _BUTTON_SERVICE],
        check=False,
        capture_output=True,
        text=True,
    )


def list_scanners(*, timeout: float = 15) -> list[str]:
    try:
        proc = subprocess.run(
            ["scanimage", "-L"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []

    devices: list[str] = []
    for line in (proc.stdout + proc.stderr).splitlines():
        match = _SCANNER_RE.search(line)
        if match and _FUJITSU_RE.search(match.group(1)):
            devices.append(match.group(1))
    return devices


def test_paperless(url: str, token: str) -> tuple[bool, str]:
    url = url.rstrip("/")
    if not url or not token:
        return False, "URL and API token are required"

    request = urllib.request.Request(
        f"{url}/api/",
        method="GET",
        headers={"Authorization": f"Token {token}"},
    )
    # Match legacy `curl -sk` for self-signed Paperless installs.
    context = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(request, context=context, timeout=5) as response:
            code = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return False, str(exc.reason)
    except TimeoutError as exc:
        return False, str(exc)

    if code == 200:
        return True, "Connected"
    return False, f"HTTP {code}"


def scanner_label(device: str) -> str:
    if not device:
        return "Auto-detect"
    short = device.split("lib=", 1)[0].rstrip(":").removeprefix("fujitsu:")
    # Drop SANE instance id (e.g. ScanSnap iX1300:11782 → ScanSnap iX1300).
    short = re.sub(r":\d+$", "", short)
    return short or device
