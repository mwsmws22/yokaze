"""Scanner connectivity indicator for the main header."""

from __future__ import annotations

import threading

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

from yokaze.lib.capture.device import seed_device_cache
from yokaze.lib.config import scanner as config

_POLL_MS = 2000
_PROBE_TIMEOUT_S = 8


class ScannerStatus(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.set_valign(Gtk.Align.CENTER)
        self.set_margin_end(6)

        self._icon = Gtk.Image.new_from_icon_name("scanner-symbolic")
        self._label = Gtk.Label(label="Detecting…")
        self._label.add_css_class("dim-label")
        self.append(self._icon)
        self.append(self._label)

        self._poll_id = 0
        self._busy = False
        self._pending = False

    def start(self, window: Gtk.Window | None = None) -> None:
        self.refresh()
        if not self._poll_id:
            self._poll_id = GLib.timeout_add(_POLL_MS, self._poll)
        if window is not None:
            window.connect("notify::is-active", self._on_window_active)

    def _on_window_active(self, window: Gtk.Window, _pspec) -> None:
        if window.is_active():
            self.refresh()

    def _poll(self) -> bool:
        if not self._busy:
            self.refresh()
        return True

    def refresh(self) -> None:
        if self._busy:
            self._pending = True
            return
        self._start_probe()

    def _start_probe(self) -> None:
        self._busy = True
        self._pending = False

        def work() -> None:
            devices = config.list_scanners(timeout=_PROBE_TIMEOUT_S)
            selected = config.load_config().scanner_device
            GLib.idle_add(self._apply, devices, selected)

        threading.Thread(target=work, daemon=True).start()

    def _apply(self, devices: list[str], selected: str) -> None:
        self._busy = False
        name, ok, tooltip = _scanner_state(devices, selected)
        self._label.set_text(name)
        self._label.set_tooltip_text(tooltip)
        self._icon.set_tooltip_text(tooltip)

        # Reuse the status probe's scanimage -L result so Capture does not
        # pay another multi-second device list when Settings is on Auto-detect.
        if ok:
            seed_device_cache(selected if selected in devices else devices[0])

        self._label.remove_css_class("warning")
        self._label.remove_css_class("success")
        if ok:
            self._icon.set_from_icon_name("scanner-symbolic")
            self._label.add_css_class("success")
        else:
            self._icon.set_from_icon_name("dialog-warning-symbolic")
            self._label.add_css_class("warning")

        if self._pending:
            self._start_probe()


def _scanner_state(
    devices: list[str], selected: str
) -> tuple[str, bool, str]:
    if selected:
        label = config.scanner_label(selected)
        if selected in devices:
            return label, True, selected
        if devices:
            found = ", ".join(config.scanner_label(d) for d in devices)
            return (
                label,
                False,
                f"Configured scanner not found. Detected: {found}",
            )
        return label, False, f"Configured scanner not found: {selected}"

    if devices:
        device = devices[0]
        label = config.scanner_label(device)
        if len(devices) == 1:
            return label, True, device
        names = ", ".join(config.scanner_label(d) for d in devices)
        return label, True, f"Auto-detect ({len(devices)} scanners): {names}"

    return "No scanner found", False, "No Fujitsu ScanSnap detected (scanimage -L)"
