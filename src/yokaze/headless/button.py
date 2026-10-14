"""Hardware button poller (yokaze-button).

Poll ``scanimage -A`` for the scan button bit, forward to a running GUI
(SIGUSR1 / ``yokaze --scan``), or run headless ``yokaze-scan`` when no
GUI is open.

Identity: Dev GUI open → ``yokaze-dev`` conf/archives; otherwise user
(including headless). One poller only — do not run two button services.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from yokaze.headless import scan as scan_cli
from yokaze.lib import paths as app_paths
from yokaze.lib.config import scanner as scanner_config
from yokaze.lib.util.errors import DeviceBusyError, YokazeError
from yokaze.lib.capture.device import (
    refresh_device,
    resolve_device,
    with_scan_lock,
)

POLL_INTERVAL = 0.1
GUI_POLL_INTERVAL = 0.5
DEBOUNCE_S = 3.0
BUTTON_STATE_RE = re.compile(r"--scan\[=\(yes\|no\)\] \[(\w+)")

_GUI_PGREP_PATTERNS = (
    r"[p]ython3? -m yokaze\.app",
    r"[p]ython3? -m yokaze\.ui",
    r"[/]yokaze(\s|$)",
    r"[/]yokaze-dev(\s|$)",
)


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def get_button_state(device: str) -> str:
    """Return ``yes`` / ``no`` / ``busy`` / ``error`` (legacy poller vocabulary)."""
    try:
        with with_scan_lock(timeout=float(os.environ.get("SCAN_LOCK_TIMEOUT", "1"))):
            proc = subprocess.run(
                ["scanimage", "--device", device, "-A"],
                capture_output=True,
                text=True,
                check=False,
            )
    except DeviceBusyError:
        return "busy"

    match = BUTTON_STATE_RE.search(proc.stdout or "")
    if match is None:
        return "error"
    return match.group(1)


def _dbus_name_owned(app_id: str) -> bool:
    if shutil.which("gdbus") is None:
        return False
    proc = subprocess.run(
        [
            "gdbus",
            "call",
            "--session",
            "--dest",
            "org.freedesktop.DBus",
            "--object-path",
            "/org/freedesktop/DBus",
            "--method",
            "org.freedesktop.DBus.NameHasOwner",
            app_id,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return "(true,)" in (proc.stdout or "")


def resolve_button_identity() -> str:
    """Dev GUI wins if open; otherwise user (GUI or headless)."""
    if _dbus_name_owned(app_paths.app_id_for(app_paths.IDENTITY_DEV)):
        return app_paths.IDENTITY_DEV
    if _dbus_name_owned(app_paths.app_id_for(app_paths.IDENTITY_USER)):
        return app_paths.IDENTITY_USER
    return app_paths.IDENTITY_USER


def _activate_identity(identity: str) -> None:
    """Switch process identity and load that identity's scanner.conf into env."""
    os.environ["YOKAZE_IDENTITY"] = identity
    scanner_config.apply_config(scanner_config.load_config())


def _pgrep_gui_pids() -> list[int]:
    pids: list[int] = []
    seen: set[int] = set()
    for pattern in _GUI_PGREP_PATTERNS:
        proc = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            text=True,
            check=False,
        )
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            pid = int(line)
            if pid not in seen and pid != os.getpid():
                seen.add(pid)
                pids.append(pid)
    return pids


def gui_running() -> bool:
    if _dbus_name_owned(app_paths.app_id_for(app_paths.IDENTITY_DEV)):
        return True
    if _dbus_name_owned(app_paths.app_id_for(app_paths.IDENTITY_USER)):
        return True
    return bool(_pgrep_gui_pids())


def _resolve_yokaze_launcher(identity: str) -> str | None:
    """Prefer installed launcher for the identity; fall back to repo launcher."""
    names = (
        ("yokaze-dev", "yokaze")
        if identity == app_paths.IDENTITY_DEV
        else ("yokaze",)
    )
    for name in names:
        which = shutil.which(name)
        if which:
            return which
    # Repo-root launcher (``./yokaze`` → ``python3 -m yokaze.ui``).
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "yokaze"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def forward_to_gui(identity: str) -> bool:
    """Ask the running app to scan. SIGUSR1 primary; launcher ``--scan`` fallback."""
    for pid in _pgrep_gui_pids():
        try:
            os.kill(pid, signal.SIGUSR1)
            return True
        except OSError:
            continue

    launcher = _resolve_yokaze_launcher(identity)
    if launcher:
        env = os.environ.copy()
        env["YOKAZE_IDENTITY"] = identity
        subprocess.Popen(
            [launcher, "--scan"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
        return True
    return False


def _notify(args: list[str]) -> None:
    if shutil.which("notify-send") is None:
        return
    try:
        subprocess.Popen(
            ["notify-send", *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass


def run_headless_scan_with_notify() -> None:
    """Headless one-button scan with desktop notifications (GUI not running)."""
    try:
        output = scan_cli.run_headless_scan()
        ok = True
    except YokazeError as exc:
        print(str(exc), file=sys.stderr)
        output = str(exc)
        ok = False

    if ok:
        _log("Scan completed successfully")
        if os.environ.get("PAPERLESS_URL"):
            _notify(
                [
                    "-i",
                    "scanner",
                    "Scan Complete",
                    "Document uploaded to Paperless",
                ]
            )
        else:
            _notify(
                [
                    "-i",
                    "scanner",
                    "Scan Complete",
                    "Document saved to folder",
                ]
            )
            _ = output  # Done: line already printed by scan_cli
    else:
        _log("Scan failed")
        _notify(
            [
                "-u",
                "critical",
                "-i",
                "dialog-error",
                "Scan Failed",
                "Something went wrong during scanning",
            ]
        )


def poll_loop(device: str) -> None:
    _log(f"Starting button poll for {device}")
    time.sleep(2)

    while True:
        poll_interval = GUI_POLL_INTERVAL if gui_running() else POLL_INTERVAL
        button_state = get_button_state(device)

        if button_state == "yes":
            identity = resolve_button_identity()
            _activate_identity(identity)
            if gui_running():
                _log(f"Button pressed! Forwarding to GUI ({identity})...")
                forward_to_gui(identity)
            else:
                _log("Button pressed! Starting headless scan (user)...")
                run_headless_scan_with_notify()
            _log("Waiting for button release...")
            time.sleep(DEBOUNCE_S)
        elif button_state == "error":
            _log("Scanner not responding, re-detecting...")
            try:
                _activate_identity(app_paths.IDENTITY_USER)
                device = refresh_device()
            except YokazeError:
                pass
            time.sleep(5)

        time.sleep(poll_interval)


def main(argv: list[str] | None = None) -> int:
    _ = argv
    # Device selection uses user conf (one scanner); per-press loads identity conf.
    _activate_identity(app_paths.IDENTITY_USER)
    try:
        device = resolve_device()
    except YokazeError as exc:
        _log(str(exc))
        return 1
    try:
        poll_loop(device)
    except KeyboardInterrupt:
        _log("Stopped")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
