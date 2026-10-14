"""Scanner and Paperless settings (yokaze.conf + systemd session env)."""

from __future__ import annotations

import os
import re
import ssl
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "environment.d"
CONFIG_PATH = CONFIG_DIR / "yokaze.conf"

AUTO_DEVICE = ""
_SCANNER_RE = re.compile(r"device `([^']+)' is a")
_FUJITSU_RE = re.compile(r"fujitsu:ScanSnap")


DEFAULT_ENHANCE_WORKERS = 1

# ADF paper geometry. Auto = 3 m ceiling + ALD; A4 = fixed A4 + bleed.
PAPER_SIZE_AUTO = "auto"
PAPER_SIZE_A4 = "a4"
PAPER_SIZES = (PAPER_SIZE_AUTO, PAPER_SIZE_A4)
DEFAULT_PAPER_SIZE = PAPER_SIZE_AUTO


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

    @property
    def is_configured(self) -> bool:
        return bool(self.paperless_url and self.paperless_token)


def _parse_enhance_workers(raw: str) -> int:
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_ENHANCE_WORKERS


def load_config() -> ScannerConfig:
    if not CONFIG_PATH.is_file():
        return ScannerConfig()

    values: dict[str, str] = {}
    for line in CONFIG_PATH.read_text().splitlines():
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
    )


def save_config(config: ScannerConfig) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    paper_size = parse_paper_size(config.paper_size)
    lines = [
        "# Yokaze scanner / Paperless configuration",
        f"SCANNER_DEVICE={config.scanner_device}",
        f"PAPERLESS_URL={config.paperless_url}",
        f"PAPERLESS_TOKEN={config.paperless_token}",
        f"SCAN_ENHANCE_WORKERS={config.enhance_workers}",
        f"DEVELOPER_MODE={'1' if config.developer_mode else '0'}",
        f"PAPER_SIZE={paper_size}",
    ]
    CONFIG_PATH.write_text("\n".join(lines) + "\n")
    apply_config(config)


def apply_config(config: ScannerConfig) -> None:
    # Set in *this* process too, so a GUI session picks up the new worker
    # count on the next scan document — no app restart needed.
    paper_size = parse_paper_size(config.paper_size)
    os.environ["SCANNER_DEVICE"] = config.scanner_device
    os.environ["PAPERLESS_URL"] = config.paperless_url
    os.environ["PAPERLESS_TOKEN"] = config.paperless_token
    os.environ["SCAN_ENHANCE_WORKERS"] = str(config.enhance_workers)
    os.environ["PAPER_SIZE"] = paper_size

    env_args = [
        f"SCANNER_DEVICE={config.scanner_device}",
        f"PAPERLESS_URL={config.paperless_url}",
        f"PAPERLESS_TOKEN={config.paperless_token}",
        f"SCAN_ENHANCE_WORKERS={config.enhance_workers}",
        f"PAPER_SIZE={paper_size}",
    ]
    subprocess.run(
        ["systemctl", "--user", "set-environment", *env_args],
        check=False,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["systemctl", "--user", "restart", "scan-button.service"],
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
    return short or device
