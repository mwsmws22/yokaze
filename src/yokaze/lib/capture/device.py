"""SANE device lock, resolve, source sensors, and scanimage batch capture."""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from yokaze.lib.util.errors import DeviceBusyError, FeederEmptyError, YokazeError

SCANSNAP_DEVICE_RE = re.compile(r"fujitsu:ScanSnap[^']+")
PAGE_LOADED_RE = re.compile(r"--page-loaded\[=\(yes\|no\)\] \[(\w+)")
CARD_LOADED_RE = re.compile(r"--card-loaded\[=\(yes\|no\)\] \[(\w+)")

BLEED_MM = 10
PAGE_WIDTH_MM = 210 + BLEED_MM
PAGE_HEIGHT_MM = 297 + BLEED_MM
# iX1300 SANE max ~3008 mm. ALD stops at the paper end within this ceiling.
# 863 mm was too short — long sheets stopped mid-feed with paper still in
# the rollers (reported as a jam). Prefer ALD + high ceiling over a low cap.
LONG_PAGE_HEIGHT_MM = 3000


def long_page_enabled() -> bool:
    """True for Auto paper (3 m + ALD). ``LONG_PAGE`` overrides ``PAPER_SIZE``."""
    if "LONG_PAGE" in os.environ:
        return os.environ.get("LONG_PAGE", "").strip().lower() in {
            "1",
            "yes",
            "true",
            "on",
        }
    # Settings default: Auto. Missing/unknown PAPER_SIZE → Auto.
    return os.environ.get("PAPER_SIZE", "auto").strip().lower() != "a4"


def scan_batch_cmd(
    device: str,
    source: str,
    prefix: str,
    *,
    long_page: bool = False,
) -> list[str]:
    """Build the ``scanimage`` argv for one batch (no subprocess)."""
    use_long = long_page and source.startswith("ADF")
    page_height = LONG_PAGE_HEIGHT_MM if use_long else PAGE_HEIGHT_MM
    cmd = [
        "scanimage",
        "--device",
        device,
        "--format=tiff",
        "--resolution",
        str(resolution()),
        "--mode",
        "Color",
        "--source",
        source,
        "--page-width",
        str(PAGE_WIDTH_MM),
        "--page-height",
        str(page_height),
        "--swdespeck",
        "2",
        "--overscan",
        "On",
        "--prepick",
        "On",
        "--buffermode",
        "On",
        f"--batch={prefix}-%04d.tiff",
        "--batch-count=-1",
    ]
    if use_long:
        # Boolean SANE options need ``--ald=yes`` as one token; ``--ald yes``
        # is parsed as a bare ``yes`` and aborts before the feed starts.
        cmd.append("--ald=yes")
    return cmd

# Last auto-detected SANE id. Avoids re-running ``scanimage -L`` (often multi-
# second when other backends like v4l are present) on every capture. Cleared /
# replaced by ``refresh_device`` when a probe fails after USB reconnect.
_cached_device: str | None = None


def clear_device_cache() -> None:
    """Test helper — drop the auto-detect cache."""
    global _cached_device
    _cached_device = None


def seed_device_cache(device: str) -> None:
    """Remember a known SANE id (e.g. from the header status probe)."""
    global _cached_device
    device = device.strip()
    if device:
        _cached_device = device


def scan_lock_path() -> Path:
    return Path(f"/run/user/{os.getuid()}/yokaze.lock")


def resolution() -> int:
    raw = os.environ.get("SCAN_RESOLUTION", "300")
    try:
        return int(raw)
    except ValueError:
        return 300


@contextmanager
def with_scan_lock(timeout: float | None = None):
    """Exclusive flock on the shared SANE lock."""
    if timeout is None:
        timeout = float(os.environ.get("SCAN_LOCK_TIMEOUT", "120"))
    lock_path = scan_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch(exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                raise DeviceBusyError(
                    "Scanner busy — could not acquire the scanner lock"
                )
            time.sleep(0.05)
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def refresh_device() -> str:
    global _cached_device
    t0 = time.perf_counter()
    proc = subprocess.run(
        ["scanimage", "-L"],
        capture_output=True,
        text=True,
        check=False,
    )
    match = SCANSNAP_DEVICE_RE.search(proc.stdout or "")
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if match is None:
        _cached_device = None
        print(f"[capture] scanimage -L failed after {elapsed_ms:.0f} ms", flush=True)
        raise YokazeError(
            "Error: no ScanSnap scanner found. Is it connected and powered on?"
        )
    _cached_device = match.group(0)
    print(
        f"[capture] scanimage -L ok in {elapsed_ms:.0f} ms → {_cached_device}",
        flush=True,
    )
    return _cached_device


def resolve_device() -> str:
    global _cached_device
    configured = os.environ.get("SCANNER_DEVICE", "").strip()
    if configured:
        return configured
    if _cached_device:
        return _cached_device
    _cached_device = refresh_device()
    return _cached_device


def resolve_source(device: str) -> str:
    """Return ``ADF Duplex`` or ``Card Duplex`` for the loaded input."""
    proc = subprocess.run(
        ["scanimage", "--device", device, "-A"],
        capture_output=True,
        text=True,
        check=False,
    )
    options = (proc.stdout or "") + (proc.stderr or "")
    lower = options.lower()
    if re.search(r"open of device .* failed|invalid argument", lower):
        raise YokazeError(
            "Error: scanner disconnected or device ID changed — reconnect the USB cable"
        )
    if not options or "page-loaded" not in options:
        raise YokazeError("Error: scanner not available (busy or disconnected?)")

    feeder = _sensor_value(PAGE_LOADED_RE, options)
    front = _sensor_value(CARD_LOADED_RE, options)
    if feeder == "yes" and front == "yes":
        raise YokazeError(
            "Error: documents loaded in both the feeder and the front slot - use only one."
        )
    if feeder == "yes":
        return "ADF Duplex"
    if front == "yes":
        return "Card Duplex"
    raise FeederEmptyError(
        "No document detected — feeder and card slot are both empty"
    )


def _sensor_value(pattern: re.Pattern[str], options: str) -> str:
    match = pattern.search(options)
    return match.group(1) if match else "no"


def next_batch_prefix(workdir: Path) -> str:
    """Return ``workdir/batch-NN-page`` for the next unused batch id."""
    existing = sorted(workdir.glob("batch-*-page-0001.tiff"))
    batch_num = len(existing) + 1
    return str(workdir / f"batch-{batch_num:02d}-page")


def scan_batch(
    device: str,
    source: str,
    prefix: str,
    *,
    long_page: bool | None = None,
) -> list[Path]:
    """Run scanimage batch capture; return written TIFF paths."""
    if long_page is None:
        long_page = long_page_enabled()
    cmd = scan_batch_cmd(device, source, prefix, long_page=long_page)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise YokazeError("scanimage not found on PATH") from exc

    output = (proc.stdout or "") + (proc.stderr or "")
    if output.strip():
        print(output.rstrip(), flush=True)

    pages = sorted(Path(prefix).parent.glob(f"{Path(prefix).name}-*.tiff"))

    if proc.returncode != 0:
        if pages and re.search(r"out of documents|no documents", output, re.I):
            return pages
        err = _scanimage_error_line(output)
        raise YokazeError(
            f"Scanner error: {err}" if err else "Scanner error — could not capture pages"
        )
    if not pages or not pages[0].is_file() or pages[0].stat().st_size == 0:
        raise YokazeError("Scanner error — could not capture pages")
    return pages


def _scanimage_error_line(output: str) -> str:
    for line in reversed(output.splitlines()):
        if re.search(
            r"scanimage:.*(error|cannot|failed|jammed|sane_start|without option)",
            line,
            re.I,
        ):
            return re.sub(r"^scanimage:\s*", "", line, flags=re.I).strip()
    return ""


def capture_pages(workdir: Path) -> tuple[list[Path], str]:
    """Lock device, resolve source, scan one batch. Returns (paths, source)."""
    workdir.mkdir(parents=True, exist_ok=True)
    t_all = time.perf_counter()

    t0 = time.perf_counter()
    device = resolve_device()
    print(
        f"[capture] resolve_device in {(time.perf_counter() - t0) * 1000:.0f} ms",
        flush=True,
    )
    prefix = next_batch_prefix(workdir)

    with with_scan_lock():
        t0 = time.perf_counter()
        device, source = _resolve_source_with_refresh(device)
        print(
            f"[capture] resolve_source in {(time.perf_counter() - t0) * 1000:.0f} ms",
            flush=True,
        )
        os.environ["SCAN_LAST_SOURCE"] = source
        os.environ["SCAN_SOURCE"] = source
        long_page = long_page_enabled() and source.startswith("ADF")
        geometry = (
            f"long≤{LONG_PAGE_HEIGHT_MM}mm+ald"
            if long_page
            else f"A4+{BLEED_MM}mm"
        )
        print(
            f"Scanning (duplex, color, {geometry}) from {source}...",
            flush=True,
        )
        t0 = time.perf_counter()
        pages = scan_batch(device, source, prefix, long_page=long_page)
        print(
            f"[capture] scan_batch in {(time.perf_counter() - t0) * 1000:.0f} ms",
            flush=True,
        )

    print(f"Scanned {len(pages)} page(s)", flush=True)
    print(
        f"[capture] total capture_pages {(time.perf_counter() - t_all) * 1000:.0f} ms",
        flush=True,
    )
    return pages, source


def _resolve_source_with_refresh(device: str) -> tuple[str, str]:
    tried_refresh = False
    while True:
        try:
            return device, resolve_source(device)
        except FeederEmptyError:
            raise
        except YokazeError:
            if tried_refresh:
                raise
            tried_refresh = True
            device = refresh_device()
